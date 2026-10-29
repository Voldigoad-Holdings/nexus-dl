#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""NexusDL build orchestrator (universal, cross-platform).

Builds, in order:

1. **sdist** (source distribution, .tar.gz) via `hatchling` or `uv build`
2. **wheel** (binary distribution, .whl) via `hatchling` or `uv build`
3. **PyInstaller** executables (per-OS) — optional, `--pyinstaller`
4. **Docker image** — optional, `--docker`

Cross-platform support:

- **Linux**: sdist, wheel, PyInstaller (`nexusdl-linux-{arch}`), Docker
- **macOS**: sdist, wheel, PyInstaller (`NexusDL.app`), Docker
- **Windows**: sdist, wheel, PyInstaller (`nexusdl-windows-{arch}.exe`)

Reproducibility:

- Uses `SOURCE_DATE_EPOCH` if set (for reproducible builds)
- Verifies checksums of downloaded artifacts
- Fails fast on any error

Usage::

    # Build sdist + wheel (default)
    python scripts/build.py

    # Build specific targets
    python scripts/build.py --target sdist
    python scripts/build.py --target wheel
    python scripts/build.py --target pyinstaller
    python scripts/build.py --target docker

    # Build everything
    python scripts/build.py --target all

    # Skip verification (faster, less safe)
    python scripts/build.py --no-verify

    # Clean before building
    python scripts/build.py --clean

    # Dry-run
    python scripts/build.py --dry-run

Exit codes::

    0   Success
    1   Build failure
    2   Invalid arguments
    3   Missing dependency (hatchling, build, pyinstaller, docker)
    4   Verification failure
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_SRC_DIR: Final[Path] = _REPO_ROOT / "src" / "nexusdl"
_DIST_DIR: Final[Path] = _REPO_ROOT / "dist"
_BUILD_DIR: Final[Path] = _REPO_ROOT / "build"
_PYPROJECT: Final[Path] = _REPO_ROOT / "pyproject.toml"
_MANIFEST: Final[Path] = _REPO_ROOT / "nexus.dl"

_VALID_TARGETS: Final[tuple[str, ...]] = (
    "sdist",
    "wheel",
    "pyinstaller",
    "docker",
    "all",
)

_VALID_ARCHS: Final[tuple[str, ...]] = ("x86_64", "arm64")

_OS_NAME: Final[str] = _detect_os()
_ARCH: Final[str] = _detect_arch()


# ────────────────────────────────────────────────────────────────────
# Utilities
# ────────────────────────────────────────────────────────────────────


def _detect_os() -> str:
    """Detect the OS."""
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "unknown"


def _detect_arch() -> str:
    """Detect the CPU architecture."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine or "unknown"


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with logging.

    Args:
        cmd: Command and arguments.
        cwd: Working directory.
        check: Raise on non-zero exit.
        capture: Capture stdout/stderr.
        env: Environment overrides.

    Returns:
        The completed process.

    Raises:
        subprocess.CalledProcessError: If ``check=True`` and exit != 0.
        FileNotFoundError: If the executable is not found.
    """
    print(f"  $ {' '.join(cmd)}")
    kwargs: dict[str, object] = {"cwd": cwd, "check": check}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    if env is not None:
        merged = os.environ.copy()
        merged.update(env)
        kwargs["env"] = merged
    return subprocess.run(cmd, **kwargs)  # type: ignore[arg-type]


def _which(name: str) -> Path | None:
    """Find an executable in PATH."""
    result = shutil.which(name)
    return Path(result) if result else None


def _read_pyproject() -> dict[str, object]:
    """Parse pyproject.toml."""
    with _PYPROJECT.open("rb") as f:
        return tomllib.load(f)


def _get_version() -> str:
    """Get the version from pyproject.toml."""
    try:
        return str(_read_pyproject()["project"]["version"])  # type: ignore[index]
    except (KeyError, TypeError):
        return "0.0.0"


def _sha256(path: Path) -> str:
    """Compute SHA-256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_iso() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _human_size(n: int) -> str:
    """Format bytes."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _format_duration(seconds: float) -> str:
    """Format seconds."""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    return f"{seconds / 60:.1f}min"


# ────────────────────────────────────────────────────────────────────
# Build result
# ────────────────────────────────────────────────────────────────────


@dataclass
class BuildResult:
    """Result of one build target.

    Attributes:
        target: Build target (``sdist``, ``wheel``, ``pyinstaller``, ``docker``).
        status: ``ok``, ``skipped``, ``failed``.
        artifacts: List of artifact paths.
        duration_seconds: Build duration.
        error: Error message if failed.
    """

    target: str
    status: str = "ok"
    artifacts: list[Path] = field(default_factory=list)
    duration_seconds: float = 0.0
    error: str | None = None


# ────────────────────────────────────────────────────────────────────
# Clean
# ────────────────────────────────────────────────────────────────────


def clean(dist: bool = True, build: bool = True, pycache: bool = True) -> None:
    """Clean build artifacts.

    Args:
        dist: Remove ``dist/``.
        build: Remove ``build/``.
        pycache: Remove ``__pycache__`` and caches.
    """
    if dist and _DIST_DIR.exists():
        print(f"  Removing {_DIST_DIR.relative_to(_REPO_ROOT)}/")
        shutil.rmtree(_DIST_DIR)
    if build and _BUILD_DIR.exists():
        print(f"  Removing {_BUILD_DIR.relative_to(_REPO_ROOT)}/")
        shutil.rmtree(_BUILD_DIR)
    if pycache:
        print("  Removing __pycache__ and caches...")
        for pattern in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"):
            for p in _REPO_ROOT.rglob(pattern):
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
        for pattern in ("*.pyc", "*.pyo", "*.pyd"):
            for p in _REPO_ROOT.rglob(pattern):
                if p.is_file():
                    p.unlink(missing_ok=True)


# ────────────────────────────────────────────────────────────────────
# sdist + wheel
# ────────────────────────────────────────────────────────────────────


def _build_with_uv() -> bool:
    """Try to build with ``uv build``.

    Returns:
        True if ``uv`` is available and build succeeded.
    """
    uv = _which("uv")
    if uv is None:
        return False
    try:
        _run([str(uv), "build", "--out-dir", str(_DIST_DIR)])
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"  ⚠️  uv build failed: {exc}", file=sys.stderr)
        return False


def _build_with_hatchling() -> bool:
    """Try to build with ``hatchling``.

    Returns:
        True if ``hatchling`` is available and build succeeded.
    """
    try:
        _run(
            [
                sys.executable,
                "-m",
                "hatchling",
                "build",
                "--target",
                "sdist",
                "--target",
                "wheel",
                "-d",
                str(_DIST_DIR),
            ]
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"  ⚠️  hatchling build failed: {exc}", file=sys.stderr)
        return False


def _build_with_python_build() -> bool:
    """Try to build with ``python -m build`` (fallback).

    Returns:
        True if ``build`` is available and build succeeded.
    """
    try:
        _run(
            [
                sys.executable,
                "-m",
                "build",
                "--outdir",
                str(_DIST_DIR),
            ]
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"  ⚠️  python -m build failed: {exc}", file=sys.stderr)
        return False


def _verify_sdist(sdist_path: Path) -> None:
    """Verify the sdist contents.

    Args:
        sdist_path: Path to the .tar.gz.

    Raises:
        RuntimeError: If verification fails.
    """
    import tarfile

    forbidden = [
        ".env",
        ".env.local",
        ".key",
        ".pem",
        "node_modules",
        ".venv",
        "library.db",
        "cookies.enc",
    ]

    with tarfile.open(sdist_path, "r:gz") as tar:
        names = tar.getnames()
        top_dir = names[0].split("/")[0] if names else ""
        base = top_dir + "/"

        for name in names:
            rel = name[len(base):] if name.startswith(base) else name
            for pattern in forbidden:
                if pattern in rel:
                    raise RuntimeError(
                        f"❌ Forbidden file in sdist: {rel} (pattern: {pattern})"
                    )

        # Required files
        required = [
            f"{base}pyproject.toml",
            f"{base}README.md",
            f"{base}LICENSE",
            f"{base}nexus.dl",
        ]
        for req in required:
            if req not in names:
                raise RuntimeError(f"❌ Required file missing from sdist: {req}")


def _verify_wheel(wheel_path: Path) -> None:
    """Verify the wheel contents.

    Args:
        wheel_path: Path to the .whl.

    Raises:
        RuntimeError: If verification fails.
    """
    import zipfile

    forbidden = [
        "interfaces/web/frontend/",
        "node_modules/",
        ".venv/",
        "__pycache__/",
    ]

    with zipfile.ZipFile(wheel_path) as zf:
        names = zf.namelist()
        for name in names:
            for pattern in forbidden:
                if pattern in name:
                    raise RuntimeError(
                        f"❌ Forbidden entry in wheel: {name} (pattern: {pattern})"
                    )

        # Required entries
        if not any(n.endswith("nexusdl/py.typed") for n in names):
            raise RuntimeError("❌ Required file missing from wheel: py.typed")

        if not any("nexusdl/data/sites.yaml" in n for n in names):
            raise RuntimeError("❌ Required file missing from wheel: data/sites.yaml")


def build_sdist_wheel(
    *, clean_first: bool = False, verify: bool = True
) -> BuildResult:
    """Build sdist and wheel.

    Tries ``uv build``, then ``hatchling``, then ``python -m build``.

    Args:
        clean_first: Clean ``dist/`` first.
        verify: Verify artifacts.

    Returns:
        The build result.
    """
    print("→ Building sdist + wheel")
    t0 = time.perf_counter()

    if clean_first:
        clean(dist=True, build=False, pycache=False)

    _DIST_DIR.mkdir(parents=True, exist_ok=True)

    # Set SOURCE_DATE_EPOCH for reproducible builds
    epoch = os.environ.get("SOURCE_DATE_EPOCH") or "1700000000"

    built = False
    with _env_override({"SOURCE_DATE_EPOCH": epoch}):
        for builder in (_build_with_uv, _build_with_hatchling, _build_with_python_build):
            if builder():
                built = True
                break

    duration = time.perf_counter() - t0

    if not built:
        return BuildResult(
            target="sdist+wheel",
            status="failed",
            duration_seconds=duration,
            error="No builder available (install uv, hatchling, or build)",
        )

    artifacts = sorted(_DIST_DIR.glob("nexusdl-*"))
    if not artifacts:
        return BuildResult(
            target="sdist+wheel",
            status="failed",
            duration_seconds=duration,
            error="Build succeeded but no artifacts found in dist/",
        )

    # Verify
    if verify:
        print("  Verifying artifacts...")
        try:
            for art in artifacts:
                if art.suffix == ".gz":
                    _verify_sdist(art)
                    print(f"  ✓ {art.name} ({_human_size(art.stat().st_size)})")
                elif art.suffix == ".whl":
                    _verify_wheel(art)
                    print(f"  ✓ {art.name} ({_human_size(art.stat().st_size)})")
        except RuntimeError as exc:
            return BuildResult(
                target="sdist+wheel",
                status="failed",
                artifacts=artifacts,
                duration_seconds=duration,
                error=str(exc),
            )

    return BuildResult(
        target="sdist+wheel",
        status="ok",
        artifacts=artifacts,
        duration_seconds=duration,
    )


# ────────────────────────────────────────────────────────────────────
# PyInstaller
# ────────────────────────────────────────────────────────────────────


def _pyinstaller_available() -> bool:
    """Check if PyInstaller is installed."""
    try:
        _run([sys.executable, "-m", "PyInstaller", "--version"], capture=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _pyinstaller_binary_name() -> str:
    """Compute the binary name per platform."""
    version = _get_version()
    if _OS_NAME == "windows":
        return f"nexusdl-windows-{_ARCH}-v{version}.exe"
    if _OS_NAME == "macos":
        return f"nexusdl-macos-{_ARCH}-v{version}"
    return f"nexusdl-linux-{_ARCH}-v{version}"


def _find_pyinstaller_output(binary_name: str) -> Path | None:
    """Find the PyInstaller output (file or .app)."""
    candidates: list[Path] = []

    if _OS_NAME == "windows":
        candidates = [
            _DIST_DIR / binary_name,
            _DIST_DIR / "nexusdl" / binary_name,
            _DIST_DIR / "nexusdl.exe",
        ]
    elif _OS_NAME == "macos":
        candidates = [
            _DIST_DIR / binary_name,
            _DIST_DIR / "NexusDL.app",
            _DIST_DIR / "nexusdl",
        ]
    else:
        candidates = [
            _DIST_DIR / binary_name,
            _DIST_DIR / "nexusdl",
        ]

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def build_pyinstaller(*, clean_first: bool = False) -> BuildResult:
    """Build a standalone executable with PyInstaller.

    Args:
        clean_first: Clean ``build/`` and ``dist/`` first.

    Returns:
        The build result.
    """
    print(f"→ Building PyInstaller executable ({_OS_NAME}/{_ARCH})")
    t0 = time.perf_counter()

    if not _pyinstaller_available():
        return BuildResult(
            target="pyinstaller",
            status="skipped",
            duration_seconds=0.0,
            error="PyInstaller not installed (pip install pyinstaller)",
        )

    if clean_first:
        clean(dist=False, build=True, pycache=False)

    # Build entry point
    entry_point = _SRC_DIR / "__main__.py"
    if not entry_point.exists():
        return BuildResult(
            target="pyinstaller",
            status="failed",
            error=f"Entry point not found: {entry_point.relative_to(_REPO_ROOT)}",
        )

    # Build command
    binary_name = _pyinstaller_binary_name()
    cmd: list[str] = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        binary_name.replace(".exe", ""),
        "--distpath",
        str(_DIST_DIR),
        "--workpath",
        str(_BUILD_DIR / "pyinstaller"),
        "--specpath",
        str(_BUILD_DIR),
        # Include the whole package
        "--collect-all",
        "nexusdl",
        # Hidden imports (dynamic)
        "--hidden-import",
        "nexusdl.interfaces.cli.cli_entry",
        "--hidden-import",
        "nexusdl.interfaces.cli.commands",
        # Strip unused
        "--exclude-module",
        "tkinter",
        "--exclude-module",
        "matplotlib",
        "--exclude-module",
        "numpy",
        "--exclude-module",
        "scipy",
        # Console app (not GUI)
        "--console",
    ]

    # Platform-specific
    if _OS_NAME == "macos":
        cmd.extend(["--windowed", "--osx-bundle-identifier", "com.voldigoadholdings.nexusdl"])
    elif _OS_NAME == "windows":
        icon = _REPO_ROOT / "assets" / "icons" / "nexusdl.ico"
        if icon.exists():
            cmd.extend(["--icon", str(icon)])
        cmd.append("--uac-admin")  # optional, comment out if needed

    cmd.append(str(entry_point))

    try:
        _run(cmd)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        return BuildResult(
            target="pyinstaller",
            status="failed",
            duration_seconds=time.perf_counter() - t0,
            error=str(exc),
        )

    duration = time.perf_counter() - t0
    output = _find_pyinstaller_output(binary_name)

    if output is None:
        return BuildResult(
            target="pyinstaller",
            status="failed",
            duration_seconds=duration,
            error=f"PyInstaller succeeded but output not found (expected {binary_name})",
        )

    # Generate checksum
    if output.is_file():
        checksum = _sha256(output)
        checksum_file = output.with_suffix(output.suffix + ".sha256")
        checksum_file.write_text(f"{checksum}  {output.name}\n", encoding="utf-8")
        artifacts = [output, checksum_file]
    else:
        # .app bundle (directory)
        artifacts = [output]

    return BuildResult(
        target="pyinstaller",
        status="ok",
        artifacts=artifacts,
        duration_seconds=duration,
    )


# ────────────────────────────────────────────────────────────────────
# Docker
# ────────────────────────────────────────────────────────────────────


def _docker_available() -> bool:
    """Check if Docker is installed and the daemon is running."""
    docker = _which("docker")
    if docker is None:
        return False
    try:
        _run([str(docker), "info"], capture=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def build_docker(
    *,
    tag: str | None = None,
    push: bool = False,
    multi_arch: bool = False,
) -> BuildResult:
    """Build the Docker image.

    Args:
        tag: Image tag (default: ``nexusdl:<version>``).
        push: Push to registry after build.
        multi_arch: Build for ``linux/amd64`` and ``linux/arm64``.

    Returns:
        The build result.
    """
    print("→ Building Docker image")
    t0 = time.perf_counter()

    if not _docker_available():
        return BuildResult(
            target="docker",
            status="skipped",
            error="Docker not available (install Docker or start the daemon)",
        )

    version = _get_version()
    tag = tag or f"nexusdl:{version}"
    dockerfile = _REPO_ROOT / "docker" / "Dockerfile"

    if not dockerfile.exists():
        return BuildResult(
            target="docker",
            status="failed",
            error=f"Dockerfile not found: {dockerfile.relative_to(_REPO_ROOT)}",
        )

    cmd: list[str] = [
        "docker",
        "build",
        "-f",
        str(dockerfile),
        "-t",
        tag,
        "--label",
        f"org.opencontainers.image.version={version}",
        "--label",
        f"org.opencontainers.image.created={_now_iso()}",
    ]

    if multi_arch:
        cmd.extend(["--platform", "linux/amd64,linux/arm64"])
        cmd.append("--push")  # multi-arch requires push

    cmd.append(str(_REPO_ROOT))

    try:
        _run(cmd)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        return BuildResult(
            target="docker",
            status="failed",
            duration_seconds=time.perf_counter() - t0,
            error=str(exc),
        )

    duration = time.perf_counter() - t0

    if push and not multi_arch:
        try:
            _run(["docker", "push", tag])
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            return BuildResult(
                target="docker",
                status="failed",
                duration_seconds=duration,
                error=f"Push failed: {exc}",
            )

    return BuildResult(
        target="docker",
        status="ok",
        duration_seconds=duration,
    )


# ────────────────────────────────────────────────────────────────────
# Context manager for env overrides
# ────────────────────────────────────────────────────────────────────


class _env_override:
    """Context manager for temporary env overrides."""

    def __init__(self, overrides: dict[str, str]) -> None:
        self._overrides = overrides
        self._original: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key, value in self._overrides.items():
            self._original[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, *args: object) -> None:
        for key, value in self._original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ────────────────────────────────────────────────────────────────────
# Report
# ────────────────────────────────────────────────────────────────────


def print_report(results: list[BuildResult], total_duration: float) -> None:
    """Print the build report.

    Args:
        results: List of build results.
        total_duration: Total duration in seconds.
    """
    print()
    print("═" * 78)
    print(f"  NexusDL Build Report — v{_get_version()}")
    print("═" * 78)
    print(f"  Platform: {_OS_NAME}/{_ARCH}")
    print(f"  Python:   {sys.version.split()[0]}")
    print()

    for r in results:
        icon = {"ok": "✅", "skipped": "⏭️ ", "failed": "❌"}.get(r.status, "❓")
        print(f"  {icon} {r.target:<18} {r.status:<10} ({_format_duration(r.duration_seconds)})")

        if r.error:
            print(f"     └─ {r.error}")

        for art in r.artifacts:
            if art.exists():
                size = art.stat().st_size if art.is_file() else _dir_size(art)
                print(f"     → {art.relative_to(_REPO_ROOT)} ({_human_size(size)})")

    print()
    print(f"  Total: {_format_duration(total_duration)}")
    print("═" * 78)


def _dir_size(path: Path) -> int:
    """Compute directory size recursively."""
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="build",
        description="NexusDL build orchestrator (universal, cross-platform)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                         # sdist + wheel
  %(prog)s --target all                            # everything
  %(prog)s --target pyinstaller                    # standalone executable
  %(prog)s --target docker --push                  # Docker + push
  %(prog)s --clean --target all                    # clean build
  %(prog)s --no-verify                             # skip verification
  %(prog)s --dry-run                               # validate only

Exit codes: 0 = success, 1 = failure, 2 = bad args, 3 = missing dependency, 4 = verification
        """,
    )

    parser.add_argument(
        "--target",
        choices=_VALID_TARGETS,
        default="sdist",
        help=f"Build target (default: sdist). Choices: {', '.join(_VALID_TARGETS)}",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean dist/, build/, __pycache__/ before building",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip artifact verification",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push Docker image to registry (implies --target docker)",
    )
    parser.add_argument(
        "--multi-arch",
        action="store_true",
        help="Build Docker for linux/amd64 + linux/arm64",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate arguments and exit",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output",
    )

    return parser


def _validate_args(args: argparse.Namespace) -> list[str]:
    """Validate arguments."""
    errors: list[str] = []

    if args.target not in _VALID_TARGETS:
        errors.append(f"Invalid target: {args.target}")

    if args.push and args.target not in ("docker", "all"):
        errors.append("--push requires --target docker (or all)")

    if args.multi_arch and args.target not in ("docker", "all"):
        errors.append("--multi-arch requires --target docker (or all)")

    if args.multi_arch and not args.push:
        errors.append("--multi-arch requires --push (Docker buildx)")

    return errors


def main() -> int:
    """CLI entry point.

    Returns:
        Exit code.
    """
    parser = build_parser()
    args = parser.parse_args()

    # Validate
    errors = _validate_args(args)
    if errors:
        for err in errors:
            print(f"❌ {err}", file=sys.stderr)
        return 2

    # Dry run
    if args.dry_run:
        print("✓ Arguments valid. Dry run complete.")
        print(f"  Target:     {args.target}")
        print(f"  Clean:      {args.clean}")
        print(f"  Verify:     {not args.no_verify}")
        print(f"  Push:       {args.push}")
        print(f"  Multi-arch: {args.multi_arch}")
        print(f"  Platform:   {_OS_NAME}/{_ARCH}")
        print(f"  Version:    {_get_version()}")
        return 0

    # Banner
    print("═" * 78)
    print(f"  NexusDL Build — v{_get_version()} on {_OS_NAME}/{_ARCH}")
    print(f"  Target: {args.target}")
    print("═" * 78)
    print()

    # Clean
    if args.clean:
        print("→ Cleaning...")
        clean()
        print()

    # Run targets
    t0 = time.perf_counter()
    results: list[BuildResult] = []

    try:
        if args.target in ("sdist", "all"):
            results.append(build_sdist_wheel(verify=not args.no_verify))

        if args.target in ("wheel",):
            results.append(build_sdist_wheel(verify=not args.no_verify))

        if args.target in ("pyinstaller", "all"):
            results.append(build_pyinstaller())

        if args.target in ("docker", "all"):
            results.append(build_docker(push=args.push, multi_arch=args.multi_arch))

    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user", file=sys.stderr)
        return 130

    duration = time.perf_counter() - t0

    # Report
    print_report(results, duration)

    # Exit code
    if any(r.status == "failed" for r in results):
        return 1
    if not any(r.status == "ok" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
