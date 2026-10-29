#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Voldigoad-Holdings
"""NexusDL development server orchestrator.

Runs the backend (FastAPI + Uvicorn) and the frontend (Next.js) in
parallel, with:

- Hot-reload on both sides
- Unified, prefixed logs
- Graceful shutdown (SIGINT/SIGTERM)
- Health-check polling
- Optional DB seeding

Usage::

    # Run backend + frontend (default)
    python scripts/dev.py

    # Backend only (useful for API work)
    python scripts/dev.py --backend-only

    # Frontend only (useful for UI work)
    python scripts/dev.py --frontend-only

    # Custom ports
    python scripts/dev.py --backend-port 8001 --frontend-port 3001

    # Clean frontend build cache before starting
    python scripts/dev.py --clean

    # Seed the DB with fake mangas
    python scripts/dev.py --seed

    # Wait until the backend is ready before starting the frontend
    python scripts/dev.py --wait-ready

    # Don't open the browser
    python scripts/dev.py --no-open

    # Skip the pnpm check (if you have a custom setup)
    python scripts/dev.py --skip-frontend-check

Exit codes::

    0   Success (clean shutdown)
    1   One process crashed
    2   Invalid arguments
    3   Missing prerequisite (pnpm, port, ...)
    130 Interrupted (Ctrl+C)

Requirements::

    - Python 3.12+
    - Node.js 20+ (frontend only)
    - pnpm 9+ (frontend only)
    - nexusdl installed in editable mode (``pip install -e ".[dev,web,cli]"``)
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# ────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_SRC_DIR: Final[Path] = _REPO_ROOT / "src"
_FRONTEND_DIR: Final[Path] = _SRC_DIR / "nexusdl" / "interfaces" / "web" / "frontend"

_DEFAULT_BACKEND_PORT: Final[int] = 8000
_DEFAULT_FRONTEND_PORT: Final[int] = 3000
_DEFAULT_BACKEND_HOST: Final[str] = "127.0.0.1"

_HEALTH_PATH: Final[str] = "/api/v1/health"
_HEALTH_TIMEOUT: Final[float] = 60.0  # seconds
_HEALTH_INTERVAL: Final[float] = 1.0  # seconds
_HEALTH_POLL_TIMEOUT: Final[float] = 3.0  # per request


# ────────────────────────────────────────────────────────────────────
# Data classes
# ────────────────────────────────────────────────────────────────────


@dataclass
class ProcessSpec:
    """Specification for a managed process.

    Attributes:
        name: Short name (e.g. ``backend``).
        command: Command list (e.g. ``["uvicorn", "app:main"]``).
        cwd: Working directory.
        env: Extra environment variables.
        url: URL to open in the browser (optional).
        health_url: URL to poll until ready (optional).
    """

    name: str
    command: list[str]
    cwd: Path
    env: dict[str, str]
    url: str | None = None
    health_url: str | None = None


# ────────────────────────────────────────────────────────────────────
# Colors
# ────────────────────────────────────────────────────────────────────

if os.environ.get("NO_COLOR"):
    _C_RESET = _C_BOLD = _C_DIM = _C_RED = _C_GREEN = _C_YELLOW = _C_BLUE = _C_CYAN = ""
else:
    _C_RESET = "\033[0m"
    _C_BOLD = "\033[1m"
    _C_DIM = "\033[2m"
    _C_RED = "\033[31m"
    _C_GREEN = "\033[32m"
    _C_YELLOW = "\033[33m"
    _C_BLUE = "\033[34m"
    _C_CYAN = "\033[36m"

_PREFIX_COLORS: Final[dict[str, str]] = {
    "backend": _C_BLUE,
    "frontend": _C_CYAN,
    "worker": _C_YELLOW,
    "system": _C_DIM,
}


# ────────────────────────────────────────────────────────────────────
# Pre-flight checks
# ────────────────────────────────────────────────────────────────────


def check_python_version() -> None:
    """Exit if Python < 3.12.

    Raises:
        SystemExit: If Python is too old.
    """
    if sys.version_info < (3, 12):
        major, minor = sys.version_info[:2]
        print(
            f"❌ Python 3.12+ required, got {major}.{minor}",
            file=sys.stderr,
        )
        sys.exit(3)


def check_nexusdl_importable() -> None:
    """Exit if ``nexusdl`` cannot be imported.

    Raises:
        SystemExit: If import fails.
    """
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    try:
        import nexusdl  # noqa: F401
    except ImportError as exc:
        print(f"❌ Cannot import nexusdl: {exc}", file=sys.stderr)
        print(
            f"   Install with: pip install -e '{_REPO_ROOT}[dev,web,cli]'",
            file=sys.stderr,
        )
        sys.exit(3)


def check_port_available(host: str, port: int, name: str) -> None:
    """Exit if the port is already in use.

    Args:
        host: Host to bind to.
        port: Port to check.
        name: Human-readable name (for the error message).

    Raises:
        SystemExit: If the port is in use.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.bind((host, port))
        except OSError:
            print(
                f"❌ {name} port {host}:{port} is already in use.",
                file=sys.stderr,
            )
            print(
                f"   Kill the process or pass a different port.",
                file=sys.stderr,
            )
            sys.exit(3)


def check_frontend_prerequisites() -> None:
    """Exit if ``pnpm`` or ``node`` are missing.

    Raises:
        SystemExit: If prerequisites are missing.
    """
    if not _FRONTEND_DIR.exists():
        print(
            f"❌ Frontend directory not found: {_FRONTEND_DIR}",
            file=sys.stderr,
        )
        print(
            "   Make sure the frontend is cloned (it's a separate sub-project).",
            file=sys.stderr,
        )
        sys.exit(3)

    if shutil.which("pnpm") is None:
        print(
            "❌ pnpm not found in PATH.",
            file=sys.stderr,
        )
        print(
            "   Install: https://pnpm.io/installation",
            file=sys.stderr,
        )
        sys.exit(3)

    if shutil.which("node") is None:
        print("❌ node not found in PATH.", file=sys.stderr)
        print("   Install Node.js 20+: https://nodejs.org/", file=sys.stderr)
        sys.exit(3)

    if not (_FRONTEND_DIR / "node_modules").exists():
        print(
            "⚠️  Frontend dependencies not installed.",
            file=sys.stderr,
        )
        print(
            f"   Run: cd {_FRONTEND_DIR} && pnpm install",
            file=sys.stderr,
        )
        print(
            "   (or rerun this script with --install-frontend)",
            file=sys.stderr,
        )


# ────────────────────────────────────────────────────────────────────
# Process management
# ────────────────────────────────────────────────────────────────────


async def stream_output(
    process: asyncio.subprocess.Process,
    name: str,
    *,
    verbose: bool = False,
) -> None:
    """Stream a subprocess's stdout/stderr with a prefix.

    Args:
        process: The subprocess.
        name: Short name (for the prefix).
        verbose: If True, print every line; else, only stderr.
    """
    color = _PREFIX_COLORS.get(name, _C_DIM)
    prefix = f"{color}[{name}]{_C_RESET}"

    async def _read(stream: asyncio.StreamReader | None, is_stderr: bool) -> None:
        if stream is None:
            return
        while True:
            try:
                line = await stream.readline()
            except (asyncio.IncompleteReadError, ValueError):
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            if not verbose and not is_stderr and not text.startswith(("ERROR", "WARNING", "Traceback")):
                # Reduce noise in default mode
                continue
            print(f"{prefix} {text}", flush=True)

    await asyncio.gather(
        _read(process.stdout, is_stderr=False),
        _read(process.stderr, is_stderr=True),
        return_exceptions=True,
    )


async def wait_for_health(url: str, timeout: float) -> bool:
    """Poll ``url`` until it returns 200 or the timeout is reached.

    Args:
        url: Full URL (e.g. ``http://127.0.0.1:8000/api/v1/health``).
        timeout: Max wait in seconds.

    Returns:
        ``True`` if the URL returned 200, ``False`` otherwise.
    """
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=_HEALTH_POLL_TIMEOUT) as resp:
                if 200 <= resp.status < 300:
                    return True
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        await asyncio.sleep(_HEALTH_INTERVAL)
    return False


async def run_process(
    spec: ProcessSpec,
    *,
    verbose: bool,
) -> int:
    """Run a process, stream its output, and return its exit code.

    Args:
        spec: The process specification.
        verbose: If True, print all output.

    Returns:
        Exit code of the process.
    """
    color = _PREFIX_COLORS.get(spec.name, _C_DIM)
    print(
        f"{color}[{spec.name}]{_C_RESET} "
        f"{_C_DIM}starting:{_C_RESET} {' '.join(spec.command)}",
        flush=True,
    )

    try:
        process = await asyncio.create_subprocess_exec(
            *spec.command,
            cwd=str(spec.cwd),
            env=spec.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        print(
            f"{_C_RED}[{spec.name}]{_C_RESET} command not found: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 3
    except OSError as exc:
        print(
            f"{_C_RED}[{spec.name}]{_C_RESET} failed to start: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    # Stream output concurrently
    stream_task = asyncio.create_task(
        stream_output(process, spec.name, verbose=verbose)
    )

    # Wait for process exit
    exit_code = await process.wait()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(stream_task, timeout=2.0)

    color_out = _C_GREEN if exit_code == 0 else _C_RED
    print(
        f"{color}[{spec.name}]{_C_RESET} "
        f"{color_out}exited with code {exit_code}{_C_RESET}",
        flush=True,
    )
    return exit_code


# ────────────────────────────────────────────────────────────────────
# Build process specs
# ────────────────────────────────────────────────────────────────────


def build_backend_spec(
    host: str,
    port: int,
    *,
    reload: bool,
    verbose: bool,
) -> ProcessSpec:
    """Build the backend process spec.

    Args:
        host: Bind host.
        port: Bind port.
        reload: Enable Uvicorn ``--reload``.
        verbose: Enable Uvicorn ``--log-level debug``.

    Returns:
        The ``ProcessSpec`` for the backend.
    """
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "nexusdl.interfaces.web.backend.main:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "debug" if verbose else "info",
    ]
    if reload:
        command.extend(["--reload", "--reload-dir", str(_SRC_DIR / "nexusdl")])

    env = os.environ.copy()
    env.setdefault("NEXUSDL_APP_ENVIRONMENT", "dev")
    env.setdefault("NEXUSDL_LOG_FORMAT", "pretty")
    env.setdefault("NEXUSDL_LOG_LEVEL", "DEBUG" if verbose else "INFO")
    env.setdefault("NEXUSDL_API_CORS_ORIGINS", '["http://localhost:3000","http://127.0.0.1:3000"]')
    # Ensure nexusdl is importable
    env["PYTHONPATH"] = str(_SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")

    return ProcessSpec(
        name="backend",
        command=command,
        cwd=_REPO_ROOT,
        env=env,
        url=f"http://{host}:{port}",
        health_url=f"http://{host}:{port}{_HEALTH_PATH}",
    )


def build_frontend_spec(
    host: str,
    backend_port: int,
    frontend_port: int,
    *,
    verbose: bool,
) -> ProcessSpec:
    """Build the frontend process spec.

    Args:
        host: Bind host.
        backend_port: Backend port (for API base URL).
        frontend_port: Frontend port.
        verbose: Pass through to pnpm (informational).

    Returns:
        The ``ProcessSpec`` for the frontend.
    """
    command = [
        "pnpm",
        "dev",
        "--hostname",
        host,
        "--port",
        str(frontend_port),
    ]

    env = os.environ.copy()
    env["NEXT_PUBLIC_API_URL"] = f"http://{host}:{backend_port}"
    env["NEXT_PUBLIC_WS_URL"] = f"ws://{host}:{backend_port}"
    env.setdefault("NODE_ENV", "development")
    if verbose:
        env["NEXT_TELEMETRY_DEBUG"] = "1"

    return ProcessSpec(
        name="frontend",
        command=command,
        cwd=_FRONTEND_DIR,
        env=env,
        url=f"http://{host}:{frontend_port}",
        health_url=None,  # Next.js doesn't expose a standard health endpoint
    )


# ────────────────────────────────────────────────────────────────────
# Seeding
# ────────────────────────────────────────────────────────────────────


async def seed_database() -> int:
    """Seed the database with fake mangas.

    Returns:
        Exit code (0 = success).
    """
    print(f"{_C_DIM}[system]{_C_RESET} seeding database...", flush=True)
    try:
        from nexusdl.core.config import load_settings
        from nexusdl.core.library.database import LibraryDatabase
        from nexusdl.core.models.manga import Manga
        from nexusdl.core.models.enums import Language, MangaStatus, ContentRating
        from datetime import datetime, timezone

        settings = load_settings()
        async with LibraryDatabase(settings.database_url) as db:
            await db.init()
            for i in range(1, 6):
                manga = Manga(
                    id=f"fake-{i:04d}",
                    source_id=f"fake-{i:04d}",
                    site="fake",
                    title=f"Fake Manga {i}",
                    author=f"Author {i}",
                    status=MangaStatus.ONGOING,
                    language=Language.EN,
                    content_rating=ContentRating.SAFE,
                    url=f"https://example.com/fake/{i}",
                    updated_at=datetime.now(tz=timezone.utc),
                )
                try:
                    await db.add_manga(manga)
                except Exception:
                    pass  # ignore duplicates
        print(
            f"{_C_GREEN}[system]{_C_RESET} seeded 5 fake mangas",
            flush=True,
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            f"{_C_RED}[system]{_C_RESET} seeding failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1


# ────────────────────────────────────────────────────────────────────
# Cleanup
# ────────────────────────────────────────────────────────────────────


def clean_frontend() -> None:
    """Remove frontend build caches (``.next/``, ``out/``, ``.turbo/``)."""
    print(f"{_C_DIM}[system]{_C_RESET} cleaning frontend build...", flush=True)
    for sub in (".next", "out", ".turbo"):
        target = _FRONTEND_DIR / sub
        if target.exists():
            try:
                shutil.rmtree(target)
                print(f"  removed {target}", flush=True)
            except OSError as exc:
                print(f"  failed to remove {target}: {exc}", file=sys.stderr, flush=True)


def install_frontend_deps() -> int:
    """Run ``pnpm install`` in the frontend directory.

    Returns:
        Exit code.
    """
    print(f"{_C_DIM}[system]{_C_RESET} installing frontend deps...", flush=True)
    try:
        result = subprocess.run(  # noqa: S603
            ["pnpm", "install"],
            cwd=str(_FRONTEND_DIR),
            check=False,
        )
        return result.returncode
    except FileNotFoundError:
        print("❌ pnpm not found", file=sys.stderr, flush=True)
        return 3


# ────────────────────────────────────────────────────────────────────
# Main orchestrator
# ────────────────────────────────────────────────────────────────────


async def async_main(args: argparse.Namespace) -> int:
    """Async main orchestrator.

    Args:
        args: Parsed CLI args.

    Returns:
        Exit code.
    """
    # Pre-flight
    check_python_version()
    check_nexusdl_importable()

    backend_spec: ProcessSpec | None = None
    frontend_spec: ProcessSpec | None = None

    if not args.frontend_only:
        check_port_available(args.backend_host, args.backend_port, "backend")
        backend_spec = build_backend_spec(
            host=args.backend_host,
            port=args.backend_port,
            reload=args.reload,
            verbose=args.verbose,
        )

    if not args.backend_only:
        if not args.skip_frontend_check:
            check_frontend_prerequisites()
        check_port_available(args.backend_host, args.frontend_port, "frontend")
        frontend_spec = build_frontend_spec(
            host=args.backend_host,
            backend_port=args.backend_port,
            frontend_port=args.frontend_port,
            verbose=args.verbose,
        )

    # Pre-actions
    if args.clean:
        clean_frontend()
    if args.install_frontend:
        rc = install_frontend_deps()
        if rc != 0:
            print("❌ pnpm install failed", file=sys.stderr)
            return rc
    if args.seed:
        rc = await seed_database()
        if rc != 0:
            return rc

    # Run processes
    tasks: dict[str, asyncio.Task[int]] = {}
    processes: dict[str, asyncio.subprocess.Process] = {}

    async def _start(spec: ProcessSpec) -> None:
        """Start a process and register its task."""
        # We use a wrapper so we can capture the Process object
        process = await asyncio.create_subprocess_exec(
            *spec.command,
            cwd=str(spec.cwd),
            env=spec.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        processes[spec.name] = process
        print(
            f"{_PREFIX_COLORS[spec.name]}[{spec.name}]{_C_RESET} "
            f"{_C_DIM}started (pid={process.pid}){_C_RESET}",
            flush=True,
        )

        # Start the stream task
        stream_task = asyncio.create_task(
            stream_output(process, spec.name, verbose=args.verbose)
        )

        # Wait for exit
        exit_code = await process.wait()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(stream_task, timeout=2.0)

        color = _C_GREEN if exit_code == 0 else _C_RED
        print(
            f"{_PREFIX_COLORS[spec.name]}[{spec.name}]{_C_RESET} "
            f"{color}exited with code {exit_code}{_C_RESET}",
            flush=True,
        )
        tasks[spec.name] = asyncio.create_task(asyncio.sleep(0, result=exit_code))
        tasks[spec.name] = asyncio.create_task(_noop(exit_code))

    async def _noop(value: int) -> int:
        return value

    if backend_spec is not None:
        await _start(backend_spec)
    if frontend_spec is not None:
        await _start(frontend_spec)

    # Optional: wait for backend health before opening browser
    if args.wait_ready and backend_spec is not None and backend_spec.health_url:
        print(
            f"{_C_DIM}[system]{_C_RESET} waiting for backend health...",
            flush=True,
        )
        ok = await wait_for_health(backend_spec.health_url, _HEALTH_TIMEOUT)
        if ok:
            print(
                f"{_C_GREEN}[system]{_C_RESET} backend is ready",
                flush=True,
            )
        else:
            print(
                f"{_C_YELLOW}[system]{_C_RESET} backend not ready after {_HEALTH_TIMEOUT}s",
                file=sys.stderr,
                flush=True,
            )

    # Open browser
    if args.open:
        urls: list[str] = []
        if frontend_spec is not None and frontend_spec.url:
            urls.append(frontend_spec.url)
        elif backend_spec is not None and backend_spec.url:
            urls.append(backend_spec.url)
        for url in urls:
            _open_browser(url)
            time.sleep(0.5)

    # Wait for all processes to exit
    try:
        await asyncio.gather(*[_wait_for_process(p) for p in processes.values()])
    except asyncio.CancelledError:
        pass

    # Gather exit codes
    exit_codes = []
    for name, process in processes.items():
        if process.returncode is not None and process.returncode != 0:
            exit_codes.append(process.returncode)

    return 1 if exit_codes else 0


async def _wait_for_process(process: asyncio.subprocess.Process) -> None:
    """Wait for a process to exit."""
    await process.wait()


def _open_browser(url: str) -> None:
    """Open ``url`` in the default browser."""
    try:
        import webbrowser

        webbrowser.open(url, new=2)
    except Exception:  # noqa: BLE001
        pass


def _shutdown(processes: dict[str, asyncio.subprocess.Process]) -> None:
    """Terminate all processes gracefully."""
    for name, process in processes.items():
        if process.returncode is None:
            print(
                f"{_C_DIM}[system]{_C_RESET} terminating {name} (pid={process.pid})",
                flush=True,
            )
            try:
                process.terminate()
            except ProcessLookupError:
                pass


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="dev",
        description="NexusDL development server (backend + frontend)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    # backend + frontend
  %(prog)s --backend-only                     # API only
  %(prog)s --frontend-only                    # UI only
  %(prog)s --clean --seed --wait-ready        # clean, seed, wait
  %(prog)s --backend-port 8001                # custom port
  %(prog)s --no-open                          # don't open browser
        """,
    )

    parser.add_argument(
        "--backend-host",
        default=_DEFAULT_BACKEND_HOST,
        help=f"Host for both servers (default: {_DEFAULT_BACKEND_HOST})",
    )
    parser.add_argument(
        "--backend-port",
        type=int,
        default=_DEFAULT_BACKEND_PORT,
        help=f"Backend port (default: {_DEFAULT_BACKEND_PORT})",
    )
    parser.add_argument(
        "--frontend-port",
        type=int,
        default=_DEFAULT_FRONTEND_PORT,
        help=f"Frontend port (default: {_DEFAULT_FRONTEND_PORT})",
    )

    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--backend-only",
        action="store_true",
        help="Run only the backend",
    )
    target.add_argument(
        "--frontend-only",
        action="store_true",
        help="Run only the frontend",
    )

    parser.add_argument(
        "--reload",
        action="store_true",
        default=True,
        help="Enable hot-reload on the backend (default: True)",
    )
    parser.add_argument(
        "--no-reload",
        action="store_false",
        dest="reload",
        help="Disable backend hot-reload",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output (all process output)",
    )
    parser.add_argument(
        "--no-open",
        action="store_false",
        dest="open",
        help="Don't open the browser",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean frontend build cache before starting",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Seed the database with fake mangas",
    )
    parser.add_argument(
        "--wait-ready",
        action="store_true",
        help="Wait for the backend health check before starting",
    )
    parser.add_argument(
        "--install-frontend",
        action="store_true",
        help="Run 'pnpm install' before starting the frontend",
    )
    parser.add_argument(
        "--skip-frontend-check",
        action="store_true",
        help="Skip the pnpm/node prerequisite check",
    )
    parser.set_defaults(open=True)

    return parser


def main() -> int:
    """CLI entry point.

    Returns:
        Exit code.
    """
    parser = build_parser()
    args = parser.parse_args()

    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print(
            f"\n{_C_YELLOW}[system]{_C_RESET} interrupted by user",
            file=sys.stderr,
            flush=True,
        )
        return 130
    except Exception as exc:  # noqa: BLE001
        print(
            f"{_C_RED}[system]{_C_RESET} fatal error: {exc}",
            file=sys.stderr,
            flush=True,
        )
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
