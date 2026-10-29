# ADR-005: Multi-Platform Builds

- **Status**: ✅ Accepted
- **Date**: 2026-10-28
- **Deciders**: Voldigoad-Holdings (lead maintainer)
- **Supersedes**: —
- **Superseded by**: —
- **Related**: [ADR-001: Hexagonal Architecture](001-hexagonal-architecture.md)

---

## Context

NexusDL targets **four platforms**:

| Platform | Primary use case |
|---|---|
| 🐧 **Linux** | Servers, self-hosting, power users |
| 🍎 **macOS** | Desktop users (Intel + Apple Silicon) |
| 🪟 **Windows** | Desktop users (10 + 11, x64 + ARM64) |
| 🐳 **Docker** | Self-hosting, NAS, CI, reproducible environments |

Additionally, NexusDL ships **four interfaces**:

- **CLI** (Typer) — terminal, all platforms
- **TUI** (Textual) — terminal, all platforms
- **GUI** (CustomTkinter) — desktop only (Linux/X11, macOS, Windows)
- **Web** (FastAPI + Next.js) — server, accessed via browser

Each interface has **different platform constraints**:

- **TUI** needs ANSI terminal support (Windows Terminal, iTerm2, GNOME Terminal)
- **GUI** needs a windowing system (X11/Wayland on Linux, Aqua on macOS, Win32 on Windows)
- **Web** needs a server (all platforms) + a browser (all platforms)
- **CLI** needs nothing beyond Python

We must decide **how to build, test, and distribute NexusDL** on all
platforms without fragmenting the codebase.

### The naive approach — "it works on my machine"

A developer on Linux writes code, runs tests locally, and pushes. CI on
Ubuntu passes. The code is merged.

**What breaks:**

- **Windows path issues** — `Path("~/Manga")` doesn't expand `~` on
  Windows the same way. `os.sep` is `\` not `/`. Case-insensitivity
  differences. `chmod` semantics are different.
- **macOS filesystem differences** — case-insensitive APFS by default
  (HFS+ historically), but case-sensitive is possible. Resource forks.
  Spotlight indexing on large downloads.
- **Line endings** — a script written with `\n` on Linux, committed as-is,
  checked out on Windows with `\r\n`, then executed with `bash` — fails.
- **Python version drift** — `python3.12` on Linux, `python` on macOS,
  `py -3.12` on Windows. Different interpreters, different `sys.stdout`
  encodings.
- **GUI availability** — CustomTkinter requires a display. On headless
  Linux (CI), `import customtkinter` fails with `TclError: no display`.
- **Docker context** — a Dockerfile that works on Linux might fail on
  macOS (buildx), and definitely fails on Windows without WSL2.

### The "just use Docker" approach

Some projects declare "Docker is the only supported platform" and skip
native builds. This is **insufficient** for NexusDL because:

- **GUI users** don't want to run an X11 server to use a desktop app.
- **CLI users** on macOS/Windows expect `pip install nexusdl` to work
  natively.
- **TUI users** want to run in their native terminal, not in a container.
- **PyPI requires** native wheels (sdist + wheel), not Docker images.

We must support **all four platforms natively**.

### Requirements

1. **Same codebase** — no platform-specific branches unless absolutely
   necessary.
2. **Native install** — `pip install nexusdl[cli]` works on Linux, macOS,
   Windows.
3. **Docker** — the web stack runs in Docker with one command.
4. **CI matrix** — every PR is tested on Linux, macOS, and Windows.
5. **Deterministic builds** — reproducibility across platforms.
6. **Line endings** — LF in git, CRLF only for `.ps1`/`.bat`/`.cmd`.
7. **Path resolution** — `platformdirs` for config/data/logs.
8. **Executables** — PyInstaller for standalone binaries per OS.
9. **Frontend** — Node.js/pnpm is universal, no native binary.

---

## Decision

We adopt a **native multi-platform strategy** with the following pillars.

### Pillar 1 — Cross-platform paths via `platformdirs`

**Never** hardcode paths like `/home/user`, `~/.config`, `C:\Users`, or
`/tmp`. Use `platformdirs`:

```python
# src/nexusdl/core/paths.py
from __future__ import annotations

from pathlib import Path

import platformdirs

APP_NAME = "nexusdl"
APP_AUTHOR = "Voldigoad-Holdings"


def user_data_dir() -> Path:
    """Return the per-user data directory, cross-platform.

    Returns:
        - Linux:   ``~/.local/share/nexusdl``
        - macOS:   ``~/Library/Application Support/nexusdl``
        - Windows: ``%LOCALAPPDATA%\\nexusdl``
    """
    return Path(platformdirs.user_data_dir(APP_NAME, APP_AUTHOR))


def user_config_dir() -> Path:
    """Return the per-user config directory, cross-platform."""
    return Path(platformdirs.user_config_dir(APP_NAME, APP_AUTHOR))


def user_log_dir() -> Path:
    """Return the per-user log directory, cross-platform."""
    return Path(platformdirs.user_log_dir(APP_NAME, APP_AUTHOR))


def user_cache_dir() -> Path:
    """Return the per-user cache directory, cross-platform."""
    return Path(platformdirs.user_cache_dir(APP_NAME, APP_AUTHOR))
```

**Rule**: every path in NexusDL goes through `core/paths.py`. No `Path.home()`,
no `os.environ["HOME"]`, no `tempfile.gettempdir()` directly.

**Enforcement**: `scripts/check_imports.py` can flag direct `Path.home()`
usage (future enhancement).

### Pillar 2 — `pathlib.Path` everywhere

**Never** use `os.path.join`, `os.sep`, or string concatenation for paths.
Use `pathlib.Path`:

```python
# ✅ RIGHT
output = dest / f"{chapter.title}.cbz"
output.parent.mkdir(parents=True, exist_ok=True)

# ❌ WRONG — fails on Windows
output = f"{dest}/{chapter.title}.cbz"
os.makedirs(os.path.dirname(output))
```

`pathlib` handles `os.sep` automatically.

**Enforcement**: `ruff` rule `PTH` (use-pathlib) is enabled in
`pyproject.toml`.

### Pillar 3 — Line endings via `.gitattributes`

Git handles line endings. `.gitattributes` ensures:

- **LF in the repo** (`* text=auto eol=lf`)
- **CRLF on checkout** only for Windows scripts:

```gitattributes
*.ps1      text eol=crlf
*.psm1     text eol=crlf
*.psd1     text eol=crlf
*.bat      text eol=crlf
*.cmd      text eol=crlf
```

Everything else (`.py`, `.yaml`, `.md`, `.sh`, `Makefile`, `Dockerfile`)
stays LF on all platforms.

**Why**: a bash script with CRLF fails on Linux (`bad interpreter:
/bin/bash^M`). A PowerShell script with LF **can** work but may fail on
older PowerShell versions — safer to use CRLF.

**Enforcement**: `.editorconfig` mirrors `.gitattributes` (both must
agree).

### Pillar 4 — CI matrix (Linux + macOS + Windows)

Every PR runs the test suite on **three operating systems**:

```yaml
# .github/workflows/test.yml (excerpt)
strategy:
  fail-fast: false
  matrix:
    os: [ubuntu-latest, macos-latest, windows-latest]
    python-version: ["3.12", "3.13"]

runs-on: ${{ matrix.os }}

steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with:
      python-version: ${{ matrix.python-version }}
  - name: Install uv
    uses: astral-sh/setup-uv@v3
  - name: Install dependencies
    run: uv sync --extra dev --extra cli
  - name: Run unit tests
    run: uv run pytest tests/unit -q
  - name: Run architecture check
    run: uv run python scripts/check_imports.py
  - name: Validate sites.yaml
    run: uv run python scripts/validate_sites.py
```

**Matrix size**: 3 OS × 2 Python versions = **6 jobs**. Each job takes
~3 minutes. Total CI cost is acceptable.

**Why not Linux-only?** Because Linux-only CI would miss:

- Path bugs (`Path.home()` returns `C:\Users\...` on Windows)
- Case-insensitivity bugs (`Path("Manga") == Path("manga")` on macOS)
- Line-ending bugs (CRLF vs LF)
- GUI import bugs (CustomTkinter requires display, but at least **import**
  must work)
- `asyncio` event loop differences (`ProactorEventLoop` on Windows vs
  `SelectorEventLoop` on Unix)

**Why not 10 OS × 5 Python versions?** Because:

- **Cost** — GitHub Actions is free for public repos, but wasted jobs
  delay feedback.
- **Diminishing returns** — the bugs we catch with 3 OS × 2 Python are
  the same bugs we'd catch with 10 OS × 5 Python.
- **CI duration** — 6 jobs finish in ~5 minutes, acceptable for PRs.

### Pillar 5 — Optional Playwright install

Playwright browsers are **large** (~150 MB per browser). We do not install
them by default. Users who need them run:

```bash
playwright install chromium
```

The `playwright` extra declares the Python package. The **browsers** are
installed separately, because:

- CLI users may not need Playwright
- Docker images can install only chromium
- CI can cache the browsers between runs

**CI**: we do **not** install Playwright browsers in the matrix. Smoke
tests that need Playwright are marked `@pytest.mark.smoke` and run only
in a **scheduled** workflow (weekly), not on every PR.

### Pillar 6 — Native executables via PyInstaller (optional)

For users who don't want Python, we provide **standalone executables**
via PyInstaller:

| Platform | Command | Output |
|---|---|---|
| Linux | `scripts/build.py --target=linux` | `dist/nexusdl-linux-x86_64` |
| macOS | `scripts/build.py --target=macos` | `dist/NexusDL.app` |
| Windows | `scripts/build.py --target=windows` | `dist/nexusdl.exe` |

**Why PyInstaller and not Nuitka?**

| Feature | PyInstaller | Nuitka |
|---|---|---|
| Compilation | Bytecode bundle | C compilation |
| Build time | ~2 min | ~20 min |
| Binary size | ~30 MB | ~15 MB |
| Compatibility | ✅ All platforms | ⚠️ macOS issues |
| Debugging | Easy (Python tracebacks) | Hard (C tracebacks) |
| CI cost | Low | High |

For NexusDL, PyInstaller is the **right trade-off**: fast builds, small
enough binaries, debuggable. Nuitka is reserved for a future optimization
if binary size becomes a problem.

**Enforcement**: `scripts/build.py` is idempotent and works on all three
OS. CI verifies each build produces a runnable binary (`nexusdl --version`
returns `0.1.0`).

### Pillar 7 — Docker multi-arch

Docker images are built for **`linux/amd64`** and **`linux/arm64`**:

```yaml
# .github/workflows/docker.yml (excerpt)
- uses: docker/build-push-action@v5
  with:
    platforms: linux/amd64,linux/arm64
    tags: ghcr.io/voldigoad-holdings/nexus-dl:latest
    push: true
```

**Why multi-arch?**

- **Apple Silicon** (M1/M2/M3) runs `linux/arm64` natively (faster)
- **Intel/AMD servers** run `linux/amd64` (the majority)
- **Raspberry Pi 4/5** runs `linux/arm64` (self-hosting)
- **AWS Graviton** runs `linux/arm64` (cheaper)

**Build time**: ~10 min per image (QEMU emulation is slow). CI caches
layers to reduce rebuilds to ~2 min.

### Pillar 8 — Frontend is universal

The Next.js frontend runs on **Node.js**, which is inherently
cross-platform. No native binary, no platform-specific code.

**Constraints**:

- Node.js 20+ (LTS)
- pnpm 9+ (fast, deterministic)
- The frontend is **excluded from the Python wheel** (`pyproject.toml`)
- Users install the frontend separately (`pnpm install` in
  `frontend/`) or via Docker

### Pillar 9 — Documentation per OS

Installation guides are **split per OS**:

```
docs/source/installation/
├── linux.md
├── macos.md
├── windows.md
└── docker.md
```

Each guide has **specific commands** (package managers, path conventions,
known issues). No "one-size-fits-all" install guide.

### Pillar 10 — `Makefile` + `justfile` (dual task runners)

- **`justfile`** — preferred, cross-platform native (Windows without
  Git Bash)
- **`Makefile`** — fallback, works on Unix (CI, macOS, Linux)

Both are **kept in sync**. Each recipe has an equivalent in the other.

**Why both?**

- `just` is **newer** and cross-platform, but not pre-installed anywhere.
- `make` is **universal** on Unix, pre-installed in CI, but requires
  Git Bash on Windows.
- Some contributors prefer `make` (muscle memory).
- Some prefer `just` (cleaner syntax).

Keeping both costs ~10 minutes of maintenance per release and eliminates
bikeshedding.

### Summary — the 10 pillars

| # | Pillar | Purpose |
|---|---|---|
| 1 | `platformdirs` for paths | No hardcoded paths |
| 2 | `pathlib.Path` everywhere | No `os.path` |
| 3 | `.gitattributes` | Line endings |
| 4 | CI matrix (3 OS × 2 Python) | Catch platform bugs |
| 5 | Optional Playwright | Don't bloat installs |
| 6 | PyInstaller (optional) | Native executables |
| 7 | Docker multi-arch | amd64 + arm64 |
| 8 | Frontend is universal | Node.js everywhere |
| 9 | Docs per OS | Clear install guide |
| 10 | `Makefile` + `justfile` | Task runners for everyone |

---

## Consequences

### Positive

- ✅ **True multi-platform** — Linux, macOS, Windows, Docker
- ✅ **No "works on my machine"** — CI catches platform bugs
- ✅ **Reproducible builds** — `.gitattributes` + `platformdirs` +
  deterministic paths
- ✅ **Native install** — `pip install nexusdl[cli]` works on all OS
- ✅ **Standalone binaries** — PyInstaller for users without Python
- ✅ **Docker multi-arch** — runs on Intel, ARM, Raspberry Pi, AWS
  Graviton
- ✅ **Clear docs** — one install guide per OS
- ✅ **No `unrar` dependency** (see [ADR-002](002-no-cbr.md)) — installs
  cleanly on Debian/Fedora/Alpine
- ✅ **Contributor-friendly** — `just` for newcomers, `make` for Unix
  veterans

### Negative

- ⚠️ **CI cost** — 6 jobs × ~3 min = ~18 min CPU per PR. Acceptable for
  a public open-source project (free on GitHub).
- ⚠️ **Slower feedback** — a Windows-only bug found in CI takes ~5 min
  vs ~1 min locally on Linux. Mitigated by `fail-fast: false` so all OS
  finish.
- ⚠️ **PyInstaller binaries are large** (~30 MB per OS). Mitigated by
  being **optional** — users can `pip install` instead.
- ⚠️ **Docker multi-arch is slow** — QEMU emulation for arm64 on x86 CI.
  Mitigated by layer caching (only rebuild changed layers).
- ⚠️ **Documentation duplication** — 4 install guides × 4 OS = more
  maintenance. Mitigated by shared snippets (`docs/_snippets/` if
  needed).
- ⚠️ **`Makefile` + `justfile` sync** — two files to maintain. Mitigated
  by CI check (`just --list` and `make -n help` must both pass).

### Neutral

- 🟡 **PyInstaller vs Nuitka** — PyInstaller is chosen for speed,
  debuggability, and cost. Nuitka is a future option.
- 🟡 **`just` adoption** — contributors install `just` once. If they
  prefer `make`, no penalty.
- 🟡 **Frontend in Docker** — the Docker web image includes both backend
  and frontend built. Non-web users don't need it.

---

## Alternatives considered

### Alternative A — "Works on my machine" (rejected)

**Rejected because:** leads to platform bugs found weeks after merge,
broken Windows/macOS users, and contributor frustration.

### Alternative B — Linux-only (rejected)

**Rejected because:** macOS and Windows are the **majority** of desktop
users. Ignoring them would exclude ~70 % of the target audience for
GUI and CLI.

### Alternative C — Docker-only (rejected)

**Rejected because:**

- GUI users don't want X11 forwarding
- CLI users expect native `pip install`
- PyPI requires native wheels (not Docker images)
- TUI users want native terminal access

### Alternative D — Separate codebases per OS (rejected)

**Rejected because:** maintenance nightmare, code duplication, impossible
to test in isolation. One codebase + CI matrix is the standard approach.

### Alternative E — WSL2-only for Windows (rejected)

**Rejected because:** WSL2 is only available on Windows 10+ (and requires
BIOS changes). Windows 7/8 users would be excluded. Many Windows users
don't want WSL2.

### Alternative F — Nuitka for binaries (rejected for now)

**Rejected because:** slower builds (~20 min vs ~2 min), macOS issues,
harder debugging. Kept as a **future option** if binary size becomes a
problem.

### Alternative G — Only `make` (rejected)

**Rejected because:** Windows users would need Git Bash or WSL. `just`
provides a native Windows experience.

### Alternative H — Only `just` (rejected)

**Rejected because:** `just` is not pre-installed in GitHub Actions, and
`make` is universal on Unix. Keeping both costs little and helps
everyone.

### Alternative I — Native multi-platform (chosen)

**Chosen because:** it is the **only** approach that satisfies all 9
requirements. It uses established tools (`platformdirs`, `pathlib`,
`.gitattributes`, CI matrix, Docker multi-arch) with minimal custom code.

---

## Examples

### Example 1 — Path resolution

```python
# ✅ RIGHT — works on all OS
from nexusdl.core.paths import user_data_dir

db_path = user_data_dir() / "library.db"
# Linux:   ~/.local/share/nexusdl/library.db
# macOS:   ~/Library/Application Support/nexusdl/library.db
# Windows: C:\Users\alice\AppData\Local\nexusdl\library.db

# ❌ WRONG — breaks on Windows
db_path = Path.home() / ".local" / "share" / "nexusdl" / "library.db"
```

### Example 2 — Line ending fix

If a contributor on Windows accidentally commits `script.sh` with CRLF:

```bash
$ file scripts/entrypoint.sh
scripts/entrypoint.sh: ASCII text, with CRLF line terminators

# Fix (Linux/macOS):
$ dos2unix scripts/entrypoint.sh
$ git add scripts/entrypoint.sh
$ git commit -m "fix(scripts): use LF line endings"

# Fix (Windows PowerShell):
PS> (Get-Content scripts\entrypoint.sh) -replace "`r`n", "`n" | Set-Content scripts\entrypoint.sh
```

`.gitattributes` **prevents** this from happening — files are checked
out with the correct endings automatically.

### Example 3 — GUI import in headless CI

`import customtkinter` fails on Linux without a display:

```
$ python -c "import customtkinter"
_tkinter.TclError: no display name and no $DISPLAY environment variable
```

**Fix**: GUI tests are guarded:

```python
# tests/unit/interfaces/test_gui.py
import pytest

pytest.importorskip("customtkinter")


@pytest.mark.skipif(
    "DISPLAY" not in os.environ and sys.platform == "linux",
    reason="GUI requires a display on Linux",
)
def test_gui_imports() -> None:
    import nexusdl.interfaces.gui.app  # noqa: F401
```

### Example 4 — Docker multi-arch

```bash
# Build for both architectures
$ docker buildx build \
    --platform linux/amd64,linux/arm64 \
    -t ghcr.io/voldigoad-holdings/nexus-dl:latest \
    -f docker/Dockerfile \
    --push .

# Pull on a Raspberry Pi (arm64) → native
$ docker pull ghcr.io/voldigoad-holdings/nexus-dl:latest
$ docker run ghcr.io/voldigoad-holdings/nexus-dl:latest nexusdl --version
0.1.0
```

### Example 5 — CI matrix output

```
test (ubuntu-latest, 3.12)    ✅ 2m 15s
test (ubuntu-latest, 3.13)    ✅ 2m 18s
test (macos-latest, 3.12)     ✅ 3m 02s
test (macos-latest, 3.13)     ✅ 3m 05s
test (windows-latest, 3.12)   ✅ 4m 12s
test (windows-latest, 3.13)   ✅ 4m 08s
```

All 6 jobs must pass before merge.

---

## References

### Tools

- **`platformdirs`**: <https://github.com/platformdirs/platformdirs>
- **`pathlib`** (stdlib): <https://docs.python.org/3/library/pathlib.html>
- **`just`** (task runner): <https://just.systems/>
- **`PyInstaller`**: <https://pyinstaller.org/>
- **`Nuitka`**: <https://nuitka.net/>
- **Docker buildx**: <https://docs.docker.com/buildx/>
- **GitHub Actions**: <https://docs.github.com/en/actions>

### Standards

- **PEP 427** — *The Wheel Binary Package Format*:
  <https://peps.python.org/pep-0427/>
- **PEP 513** — *A Platform Tag for Portable Linux*:
  <https://peps.python.org/pep-0513/>
- **manylinux**: <https://github.com/pypa/manylinux>
- **POSIX** — for line endings and path semantics

### Platform-specific

- **X11 / Wayland** — Linux GUI: <https://wayland.freedesktop.org/>
- **Aqua** — macOS GUI: <https://developer.apple.com/design/>
- **Win32** — Windows GUI:
  <https://learn.microsoft.com/en-us/windows/win32/>
- **Windows ProactorEventLoop**:
  <https://docs.python.org/3/library/asyncio-platforms.html#windows>

### Related ADRs

- [ADR-001: Hexagonal Architecture](001-hexagonal-architecture.md)
- [ADR-002: No CBR Support](002-no-cbr.md)
- [ADR-003: Parser Factory DI](003-parser-factory-di.md)
- [ADR-004: sites.yaml vs .env](004-sites-yaml-vs-env.md)

### Related docs

- `docs/source/installation/linux.md`
- `docs/source/installation/macos.md`
- `docs/source/installation/windows.md`
- `docs/source/installation/docker.md`
- `.github/workflows/test.yml` — CI matrix
- `.github/workflows/build.yml` — PyInstaller builds
- `.github/workflows/docker.yml` — multi-arch images
- `scripts/build.py` — PyInstaller wrapper

---

## Review and amendments

This ADR is **living** — if we adopt Nuitka, drop `make`, or change the
CI matrix, we will write a new ADR (e.g.
`006-nuitka-binaries.md`) that **extends** this one.

**Amendments log:**

| Date | Change | ADR |
|---|---|---|
| 2026-10-28 | Initial version | 005 |
| — | (none yet) | — |

---

## How to verify compliance

```bash
# 1. Ensure no hardcoded paths
grep -rE 'Path\.home\(\)|/home/|C:\\\\Users|~/\.local' \
  src/nexusdl/ --include="*.py" \
  && echo "❌ Hardcoded path detected" \
  || echo "✅ No hardcoded path"

# 2. Ensure pathlib is used (no os.path.join)
grep -rE 'os\.path\.(join|sep|exists)' \
  src/nexusdl/ --include="*.py" \
  && echo "❌ os.path used (use pathlib)" \
  || echo "✅ pathlib used"

# 3. Ensure .gitattributes exists and covers key extensions
test -f .gitattributes \
  && echo "✅ .gitattributes present" \
  || echo "❌ .gitattributes missing"

grep -q "text eol=crlf" .gitattributes \
  && echo "✅ CRLF rules present" \
  || echo "❌ CRLF rules missing"

# 4. Ensure CI matrix runs 3 OS
grep -q "ubuntu-latest" .github/workflows/test.yml \
  && grep -q "macos-latest" .github/workflows/test.yml \
  && grep -q "windows-latest" .github/workflows/test.yml \
  && echo "✅ CI matrix covers 3 OS" \
  || echo "❌ CI matrix incomplete"

# 5. Ensure Docker multi-arch
grep -q "linux/amd64,linux/arm64" .github/workflows/docker.yml \
  && echo "✅ Docker multi-arch enabled" \
  || echo "❌ Docker multi-arch missing"

# 6. Ensure platformdirs is used
grep -rE "from platformdirs|import platformdirs" \
  src/nexusdl/core/ --include="*.py" \
  && echo "✅ platformdirs used" \
  || echo "❌ platformdirs missing"
```

Expected output:

```
✅ No hardcoded path
✅ pathlib used
✅ .gitattributes present
✅ CRLF rules present
✅ CI matrix covers 3 OS
✅ Docker multi-arch enabled
✅ platformdirs used
```

---

## Summary

| Aspect | Decision |
|---|---|
| Platforms | Linux, macOS, Windows, Docker |
| Paths | `platformdirs` + `pathlib.Path` |
| Line endings | `.gitattributes` (LF except `.ps1`/`.bat`) |
| CI | 3 OS × 2 Python versions (6 jobs) |
| Playwright | Optional install (`playwright install chromium`) |
| Executables | PyInstaller (Linux, macOS, Windows) |
| Docker | Multi-arch (`linux/amd64`, `linux/arm64`) |
| Frontend | Node.js/pnpm (universal), excluded from Python wheel |
| Docs | One install guide per OS |
| Task runners | `justfile` (preferred) + `Makefile` (fallback) |
| Alternative rejected | Linux-only, Docker-only, separate codebases, WSL2-only, Nuitka |

---

*This ADR is part of NexusDL and is licensed under
[GPL-3.0-or-later](../../../../LICENSE).*
